#!/usr/bin/env python3
"""OpenClaw ↔ 律所大脑 的桥接进程。

## 它在整套东西里的位置

    客户在微信/美团里说话
        → OpenClaw（一只手，负责收发）
        → 【本脚本】
        → 律所服务器 /channel/inbound（大脑：判断、生成、合规、评分、派单）
        → 【本脚本】
        → OpenClaw 把话打回平台

**这个脚本跑在 OpenClaw 那台机器上，不是跑在律所服务器上。** 这不是部署偏好，
是安全边界：律所服务器上存着当事人的咨询记录（保密义务 + PIPL），而 OpenClaw
是一个能自己写代码、装了几十个集成的代理运行时。两者必须在不同的机器上，
中间只留这一条窄口子。

## 为什么要有这个桥，而不是让 OpenClaw 直接调我们的接口

三件它必须替我们做的事：

1. **重试与不丢消息。** 网络抖一下，客户那句话就没了——而客户还在等。
2. **销账（ack）。** 我们那侧「宁可重发一句，不可丢一句」，没人 ack 就会重发。
3. **心跳。** OpenClaw 停了是静默失败：客户在等，两边后台都一片安静。

## 关于「发送接口」的确切形状

OpenClaw 的文档站在本开发环境不可达（出网策略拒绝），因此**发送接口做成配置项**，
和抖音那条通道当初的处理一样：先按最常见的形状实现，凭据到手后跑

    python scripts/openclaw_bridge.py --probe

用真实返回校正 `OPENCLAW_SEND_URL` / `OPENCLAW_SEND_SHAPE`，不必改代码。
所有 OpenClaw 特有的东西都收在 `OpenClawAdapter` 一个类里——**换成影刀或别的手，
只改这个类，别的一行不动。**
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger("openclaw-bridge")

# ---------------------------------------------------------------- 配置
BRAIN_URL = os.environ.get("BRAIN_URL", "").rstrip("/")
CHANNEL_TOKEN = os.environ.get("CHANNEL_TOKEN", "")
CHANNEL = os.environ.get("CHANNEL", "wechat")
CHANNEL_LABEL = os.environ.get("CHANNEL_LABEL", "")
OPENCLAW_SEND_URL = os.environ.get(
    "OPENCLAW_SEND_URL", "http://127.0.0.1:8080/api/messages/send"
)
OPENCLAW_TOKEN = os.environ.get("OPENCLAW_TOKEN", "")
LISTEN_PORT = int(os.environ.get("BRIDGE_PORT", "8790"))
# 分条之间停一下：三条消息同一秒刷出来不像人
SEND_GAP_SECONDS = float(os.environ.get("SEND_GAP_SECONDS", "1.5"))
HEARTBEAT_SECONDS = int(os.environ.get("HEARTBEAT_SECONDS", "180"))
TIMEOUT = 20


def _post(url: str, payload: dict, headers: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    for k, v in headers.items():
        if v:
            req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        raw = r.read().decode("utf-8", "replace")
    return json.loads(raw) if raw.strip() else {}


# ---------------------------------------------------------------- 大脑侧
class Brain:
    """律所服务器。协议见 docs/channels.md，稳定，不随 OpenClaw 版本变。"""

    def __init__(self, base: str = "", token: str = ""):
        self.base = (base or BRAIN_URL).rstrip("/")
        self.token = token or CHANNEL_TOKEN

    @property
    def _head(self) -> dict:
        return {"X-Channel-Token": self.token}

    def inbound(self, external_id: str, content: str, *, msg_id: str = "",
                name: str = "", channel: str = "") -> list[dict]:
        data = _post(f"{self.base}/channel/inbound", {
            "channel": channel or CHANNEL,
            "external_id": external_id,
            "content": content,
            "msg_id": msg_id,
            "name": name,
            "label": CHANNEL_LABEL,
        }, self._head)
        return data.get("replies", []) or []

    def pending(self, channel: str = "") -> list[dict]:
        """谁在等我们说话。挽留、跟进这类**主动发起**的话全靠它。"""
        url = f"{self.base}/channel/pending?channel={channel or CHANNEL}"
        req = urllib.request.Request(url)
        for k, v in self._head.items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8", "replace") or "{}")
        return data.get("conversations", []) or []

    def outbox(self, external_id: str, channel: str = "") -> list[dict]:
        url = (f"{self.base}/channel/outbox?channel={channel or CHANNEL}"
               f"&external_id={urllib.parse.quote(external_id)}")
        req = urllib.request.Request(url)
        for k, v in self._head.items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8", "replace") or "{}")
        return data.get("replies", []) or []

    def ack(self, ids: list[int]) -> None:
        if ids:
            _post(f"{self.base}/channel/ack",
                  {"ids": ids, "channel": CHANNEL}, self._head)

    def heartbeat(self) -> None:
        _post(f"{self.base}/channel/heartbeat",
              {"channel": CHANNEL, "label": CHANNEL_LABEL}, self._head)


# ---------------------------------------------------------------- 手侧
class OpenClawAdapter:
    """**所有 OpenClaw 特有的东西都在这个类里。**

    换影刀、换别的 RPA、换自研脚本，只改这一个类——这是整套多渠道方案
    能不被任何一家工具绑死的原因。
    """

    # 发送请求体的形状。文档站不可达，故列出几种常见形状由配置切换，
    # --probe 用真实返回确定用哪个，不必改代码重新部署。
    SHAPES = {
        "default": lambda to, text: {"to": to, "text": text},
        "chat": lambda to, text: {"chatId": to, "message": text},
        "session": lambda to, text: {"session": to, "content": text},
    }

    def __init__(self, send_url: str = "", token: str = "", shape: str = ""):
        self.send_url = send_url or OPENCLAW_SEND_URL
        self.token = token or OPENCLAW_TOKEN
        self.shape = shape or os.environ.get("OPENCLAW_SEND_SHAPE", "default")

    def parse_inbound(self, payload: dict) -> dict | None:
        """把 OpenClaw 推来的 webhook 归一成我们要的四个字段。

        字段名按常见别名逐个试——平台改版加个前缀是常事，写死一个键
        会在某次升级后**静默地**变成「一条消息都收不到」。
        """
        def pick(*names, default=""):
            for n in names:
                v = payload.get(n)
                if isinstance(v, dict):  # 有些实现把正文套一层
                    v = v.get("text") or v.get("content") or v.get("body")
                if isinstance(v, str) and v.strip():
                    return v.strip()
            return default

        external_id = pick("from", "sender", "chatId", "chat_id", "session",
                           "senderId", "peer")
        content = pick("text", "content", "message", "body")
        if not external_id:
            return None
        return {
            "external_id": external_id,
            "content": content,
            "msg_id": pick("id", "msgId", "msg_id", "messageId"),
            "name": pick("senderName", "name", "nickname"),
        }

    def send(self, to: str, text: str) -> bool:
        maker = self.SHAPES.get(self.shape) or self.SHAPES["default"]
        try:
            _post(self.send_url, maker(to, text),
                  {"Authorization": f"Bearer {self.token}" if self.token else ""})
            return True
        except Exception as e:
            logger.error("发送失败（%s）：%s", self.send_url, e)
            return False


# ---------------------------------------------------------------- 主循环
def handle_one(brain: Brain, hand: OpenClawAdapter, payload: dict) -> dict:
    """收一条 → 交给大脑 → 把话打回去 → 销账。

    **发失败就不销账**：下一轮还能取到，比丢了强。
    """
    parsed = hand.parse_inbound(payload)
    if parsed is None:
        return {"ok": False, "reason": "认不出发信人（检查字段映射）"}
    try:
        replies = brain.inbound(**parsed)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        # 大脑连不上不能吞掉客户这句话——返回失败让 OpenClaw 重投
        logger.error("大脑连不上：%s", e)
        return {"ok": False, "reason": "brain unreachable"}

    sent_ids = []
    for i, r in enumerate(replies):
        if i and SEND_GAP_SECONDS > 0:
            time.sleep(SEND_GAP_SECONDS)
        if hand.send(parsed["external_id"], r.get("text", "")):
            sent_ids.append(r.get("id"))
        else:
            break  # 首条就发不出去，后面几条只会继续失败
    try:
        brain.ack([i for i in sent_ids if isinstance(i, int)])
    except Exception:
        logger.warning("销账失败，这几条下轮会重发（宁可重发不可丢）")
    return {"ok": True, "sent": len(sent_ids), "queued": len(replies)}


def deliver_pending(brain: Brain, hand: OpenClawAdapter) -> int:
    """把「客户没说话、但我们该说话」的那几句送出去。返回送出条数。

    没有这一步，挽留话术会静静躺在发件箱里直到客户自己再开口——
    而他要是再开口，挽留就没有意义了。这是**主动发起**唯一的出口。
    """
    sent = 0
    try:
        waiting = brain.pending()
    except Exception as e:
        logger.warning("取待发清单失败：%s", e)
        return 0
    for convo in waiting:
        external_id = convo.get("external_id") or ""
        if not external_id:
            continue
        try:
            replies = brain.outbox(external_id)
        except Exception:
            continue
        ok_ids = []
        for i, r in enumerate(replies):
            if i and SEND_GAP_SECONDS > 0:
                time.sleep(SEND_GAP_SECONDS)
            if not hand.send(external_id, r.get("text", "")):
                break
            ok_ids.append(r.get("id"))
        if ok_ids:
            sent += len(ok_ids)
            try:
                brain.ack([i for i in ok_ids if isinstance(i, int)])
            except Exception:
                logger.warning("销账失败，这几条下轮会重发")
    return sent


def poll_loop(interval: int = 30) -> None:
    """只轮询、不收 webhook。**美团这类平台就该用这个模式**——
    那边没有 webhook 可推，消息是被抓下来的，主动发起也只能靠定期问。
    """
    brain, hand = Brain(), OpenClawAdapter()
    last_beat = 0.0
    while True:
        try:
            n = deliver_pending(brain, hand)
            if n:
                logger.info("主动送出 %s 条", n)
            if time.monotonic() - last_beat > HEARTBEAT_SECONDS:
                last_beat = time.monotonic()
                brain.heartbeat()
        except Exception:
            logger.exception("轮询一轮失败，继续下一轮")
        time.sleep(max(5, interval))


def serve(port: int = LISTEN_PORT) -> None:
    """收 OpenClaw 的 webhook。用标准库，不给那台机器再装一堆依赖。"""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    brain, hand = Brain(), OpenClawAdapter()
    last_beat = [0.0]

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # 默认日志太吵，接管掉
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                payload = {}
            try:
                out = handle_one(brain, hand, payload)
            except Exception as e:
                logger.exception("处理失败")
                out = {"ok": False, "reason": str(e)[:200]}
            # 心跳搭车发：没有它，「今天没客户」和「三天前就挂了」长得一模一样。
            # 顺手把主动发起的话也送掉——挽留那几句没人来取就永远发不出去。
            if time.monotonic() - last_beat[0] > HEARTBEAT_SECONDS:
                last_beat[0] = time.monotonic()
                try:
                    brain.heartbeat()
                    deliver_pending(brain, hand)
                except Exception:
                    pass
            body = json.dumps(out, ensure_ascii=False).encode()
            self.send_response(200 if out.get("ok") else 502)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    logger.info("桥接就绪：把 OpenClaw 的 webhook 指到 http://<本机>:%s/", port)
    HTTPServer(("0.0.0.0", port), H).serve_forever()


def probe() -> int:
    """上线前自检：三件事任一不通，跑起来都是白跑。"""
    ok = True
    print("=" * 56)
    if not BRAIN_URL or not CHANNEL_TOKEN:
        print("✗ 没配 BRAIN_URL / CHANNEL_TOKEN")
        return 1
    brain = Brain()
    try:
        brain.heartbeat()
        print(f"✓ 大脑连得上，渠道「{CHANNEL}」心跳已记录")
    except Exception as e:
        print(f"✗ 大脑连不上：{e}")
        ok = False
    try:
        replies = brain.inbound("probe-user", "这是一条连通性自检消息，请忽略",
                                msg_id="probe-1")
        print(f"✓ 判断链跑通，本次返回 {len(replies)} 条待发")
    except Exception as e:
        print(f"✗ 判断链不通：{e}")
        ok = False
    hand = OpenClawAdapter()
    print(f"· 发送地址：{hand.send_url}（形状 {hand.shape}）")
    print("  用 --send-test 拿真实账号试一条，再定 OPENCLAW_SEND_SHAPE")
    print("=" * 56)
    return 0 if ok else 1


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    p = argparse.ArgumentParser(description="OpenClaw ↔ 律所大脑 桥接")
    p.add_argument("--probe", action="store_true", help="连通性自检")
    p.add_argument("--send-test", metavar="TO", help="给这个会话试发一条")
    p.add_argument("--poll", action="store_true",
                   help="只轮询不收 webhook（美团这类抓取式渠道用这个）")
    p.add_argument("--interval", type=int, default=30, help="轮询间隔秒")
    p.add_argument("--port", type=int, default=LISTEN_PORT)
    args = p.parse_args(argv)
    if args.probe:
        return probe()
    if args.poll:
        poll_loop(args.interval)
        return 0
    if args.send_test:
        ok = OpenClawAdapter().send(args.send_test, "连通性测试，请忽略。")
        print("✓ 发出去了" if ok else "✗ 没发出去，检查 OPENCLAW_SEND_URL / SHAPE")
        return 0 if ok else 1
    serve(args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())

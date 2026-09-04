"""抖音企业号私信通道：接收私信/进入会话事件，回复私信。

为什么值得单开一条通道（业务依据，2026-08）：
用户第一次接触律所的场景其实不在微信 —— 直播和主页私信才是 first pass 的入口，
微信客服要客户先扫码，多一个动作。抖音后台实测漏斗：
进私 416 → 开口 90（21.6%）→ 留资 50。前两段的断点正是 AI 能补的。

与微信客服的关键差异（全部是平台硬限制，不是设计选择）：

1. **只能回复，不能主动发起**：用户发消息后 24 小时内才允许发，超时接口直接拒。
2. **回复条数有限**：同一个 24 小时窗口内、用户下次开口之前，最多发 6 条。
   我们的分条发送（一条回复拆成 1~3 条）在这里必须收敛，否则两轮就打满配额，
   真正要紧的话（要电话、邀约到所）反而发不出去。
3. **进入会话页事件要求 30 秒内响应**：所以进线问候必须走异步 worker，
   不能等 LLM ——我们的问候本来就是确定性模板，天然满足。
4. **仅认证企业号（蓝V）可用**，且权限要在开发者后台单独申请、过审。

接口凭据用 client_token（应用级），不是用户授权的 access_token ——
私信是「企业号自己的会话」，不需要逐个用户授权。

⚠️ 发送接口的确切路径与请求体字段以官方文档为准：抖音文档站在本环境不可达
（出网策略 403），故 send_url 走配置项 `RESPONDER_DOUYIN_SEND_URL`，
凭据到手后跑 `python scripts/douyin_smoke.py` 校正，无需改代码。
"""

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass

import httpx

from responder.config import Settings, get_settings
from responder.models import IncomingMessage

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://open.douyin.com/oauth/client_token/"

# 回调事件名。抖音在不同文档版本/应用类型下用过驼峰与下划线两种写法，全都认——
# 认漏一个的后果是客户私信进来我们收不到，比多认一个严重得多。
EVENT_RECEIVE_MSG = ("imReceiveMsg", "im_receive_msg", "receive_msg")
EVENT_ENTER_CHAT = (
    "imEnterDirectMessage",
    "im_enter_direct_message",
    "enter_direct_message",
    "enter_chat",
)
# 配置回调地址时平台发来的挑战包，原样回显 challenge 才算配置成功
EVENT_VERIFY = ("verify_webhook", "verify")


_SIG_HEADERS = ("x-douyin-signature", "x-douyin-msg-signature", "msg-signature")


def verify_signature(token: str, headers, body: bytes) -> bool:
    """校验回调签名：请求确实来自抖音，而不是任何知道我们地址的人。

    抖音在不同应用类型/文档版本里用过几种拼法（拼接顺序、是否含 body、
    是否先排序），官方文档站在本环境不可达，无法确定唯一一种。
    因此这里对**所有已知拼法**取 SHA1 逐一比对。

    这不会削弱安全性：每一种拼法都必须先持有校验 Token 才能算出来，
    攻击者没有 Token 一种也凑不出。等能拿到官方文档后收敛成唯一一种即可
    （届时只改这一个函数，调用方不动）。
    """
    got = ""
    for name in _SIG_HEADERS:
        got = headers.get(name) or headers.get(name.title()) or ""
        if got:
            break
    if not got:
        return False
    ts = headers.get("x-douyin-timestamp") or headers.get("timestamp") or ""
    nonce = headers.get("x-douyin-nonce") or headers.get("nonce") or ""
    text = body.decode("utf-8", "ignore")
    candidates = (
        "".join(sorted([token, ts, nonce, text])),
        token + ts + nonce + text,
        "".join(sorted([token, ts, nonce])),
        token + ts + nonce,
        token + text,
    )
    expected = {hashlib.sha1(c.encode()).hexdigest() for c in candidates}
    return any(hmac.compare_digest(got.lower(), e) for e in expected)


def conversation_id(open_id: str) -> str:
    """会话档案主键。

    与导入的抖音客资（`dy:{手机号}`）刻意分开前缀：那边一行是一条历史留资记录，
    这边是一个活的对话。同一个人两边都有时，靠联系方式在线索层合并
    （`Store.find_lead_by_contact`），不靠主键撞车。
    """
    return f"dyim:{open_id}"


@dataclass
class DouyinEnvelope:
    """一条私信事件 + 建档/回复所需的会话元信息。"""

    msg: IncomingMessage | None = None
    open_id: str = ""
    conversation_short_id: str = ""
    nickname: str = ""
    event_type: str = ""
    challenge: int | None = None

    @property
    def group_id(self) -> str:
        return conversation_id(self.open_id) if self.open_id else ""

    @property
    def is_enter(self) -> bool:
        return self.event_type in EVENT_ENTER_CHAT


def _first(d: dict, *names: str, default=""):
    """抖音各版本文档里同一含义的字段名不完全一致，按优先级取第一个非空的。"""
    for n in names:
        v = d.get(n)
        if v not in (None, "", 0):
            return v
    return default


def parse(payload: dict, *, fallback_msg_id: str = "") -> DouyinEnvelope | None:
    """回调 JSON → DouyinEnvelope；无法处理的报文返回 None。

    抖音把业务字段放在 `content` 里，且**常常是一个 JSON 字符串**而不是对象，
    两种都得吃下：只支持其中一种，线上换个版本就整条通道哑掉。
    """
    event = str(_first(payload, "event", "event_type", "Event"))
    content = payload.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (ValueError, TypeError):
            content = {}
    if not isinstance(content, dict):
        content = {}

    if event in EVENT_VERIFY:
        challenge = content.get("challenge")
        try:
            return DouyinEnvelope(event_type=event, challenge=int(challenge))
        except (TypeError, ValueError):
            return None

    open_id = str(
        _first(payload, "from_user_id", "open_id")
        or _first(content, "from_user_id", "open_id", "sec_open_id")
    )
    if not open_id:
        return None

    env = DouyinEnvelope(
        open_id=open_id,
        conversation_short_id=str(
            _first(content, "conversation_short_id", "conversation_id")
        ),
        nickname=str(_first(content, "nickname", "user_name", "name")),
        event_type=event,
    )
    if env.is_enter:
        return env
    if event not in EVENT_RECEIVE_MSG:
        return None

    # 文本以外（图片/语音/视频）没有可判断的文字，仍要建档 + 转人工，不能静默丢弃
    msg_type = str(_first(content, "message_type", "msg_type", default="text"))
    text = ""
    if msg_type == "text":
        raw = content.get("text") or content.get("content") or ""
        if isinstance(raw, dict):
            raw = raw.get("text") or raw.get("content") or ""
        text = str(raw)

    env.msg = IncomingMessage(
        msg_id=str(_first(content, "server_message_id", "msg_id") or fallback_msg_id),
        group_id=env.group_id,
        sender_id=open_id,
        sender_is_staff=False,
        content=text,
        msg_type="text" if msg_type == "text" else (msg_type or "other"),
    )
    return env if env.msg.msg_id else None


class DouyinClient:
    """抖音开放平台客户端。收由回调驱动；发受运行模式门控（见 service.Pipeline）。"""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._token: str = ""
        self._token_expiry: float = 0.0

    def available(self) -> bool:
        return bool(self.settings.douyin_client_key and self.settings.douyin_client_secret)

    def client_token(self) -> str:
        """应用级凭据。私信是企业号自己的会话，不需要逐个用户授权。"""
        if self._token and time.time() < self._token_expiry:
            return self._token
        resp = httpx.post(
            _TOKEN_URL,
            json={
                "grant_type": "client_credential",
                "client_key": self.settings.douyin_client_key,
                "client_secret": self.settings.douyin_client_secret,
            },
            timeout=10,
        ).json()
        data = resp.get("data") or resp
        token = data.get("access_token") or ""
        if not token:
            raise RuntimeError(f"douyin client_token failed: {resp}")
        self._token = token
        # 抖音签发 7200 秒，留 120 秒余量，避免边界上拿着刚过期的令牌去发
        self._token_expiry = time.time() + float(data.get("expires_in", 7200)) - 120
        return token

    def send_text(self, open_id: str, text: str) -> bool:
        """回复一条私信。失败只记日志返回 False，由控制台待办与人工兜底。"""
        try:
            resp = httpx.post(
                self.settings.douyin_send_url,
                headers={
                    "access-token": self.client_token(),
                    "Content-Type": "application/json",
                },
                json={
                    "to_user_id": open_id,
                    "message_type": "text",
                    "content": json.dumps({"text": text}, ensure_ascii=False),
                },
                timeout=15,
            ).json()
        except Exception:
            logger.exception("douyin send_msg error (open_id=%s)", open_id[:12])
            return False
        data = resp.get("data") or resp
        code = data.get("error_code", data.get("err_no", 0))
        if code:
            logger.error("douyin send_msg rejected: %s", resp)
            return False
        return True

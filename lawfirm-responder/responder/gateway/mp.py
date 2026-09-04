"""微信公众号（服务号）通道——酷机时代售后的主通道。

## 为什么是这条路

酷机时代的企业微信受品牌方管控，开不了微信客服。但他们有一个**已认证的服务号，
两万多关注用户**。而认证服务号的「客服消息」接口，是微信官方给的
**服务端全自动通道**：不需要任何人点确认，跟企微客服是同一个量级的能力。

而且它比企微客服那条路少了整整一个环节：**不用引流**。
那两万人已经在里面了——AI 上线当天就有真实客户可服务，
不用等销售改流程、不用赌客户点不点那一下。

## 平台硬限制（写死在代码里，不得放宽）

已核腾讯官方文档（`developers.weixin.qq.com` 客服消息接口）：

| 触发 | 窗口 | 额度 |
|---|---|---|
| 用户给公众号发消息 | **48 小时** | **5 条** |
| 关注 / 扫码 / 点菜单 | **1 分钟** | 3 条 |

与抖音那条通道同一个道理：**超发不是「多发了一条」，是接口报错 + 号被平台标记。**
所以 `Budget` 宁可少算一条，也不赌。记账逻辑与 `service._douyin_budget` 同源。

## 两条发送路径，区别很大

1. **被动回复**：在回调的 HTTP 响应里直接返回一段 XML。
   **不消耗那 5 条额度**，但微信要求 5 秒内响应，超时用户会看到「该公众号提供的服务出现故障」。
2. **客服消息**：异步调 `message/custom/send`。消耗额度，但没有时间压力。

第一期**全部走客服消息**：判断层虽快，但话术若要模型润色就可能超过 5 秒，
而「服务出现故障」那五个字比慢两秒难看得多。被动回复留给以后做确定性话术的优化。

## 与企微的三处关键差异（踩过的人才知道）

- **回调是 XML，不是 JSON**，字段名首字母大写（`FromUserName` / `MsgType`）；
- **验签算法不同**：公众号是 `token/timestamp/nonce` 三个值排序后拼接取 SHA1，
  没有 `msg_signature` 那一套（明文模式下）；安全模式才加 `Encrypt`；
- **客户标识是 openid**，且**同一个人在不同公众号下的 openid 不同**——
  所以它只能在本号内当主键用，不能拿去跟企微那边的客户对应。
"""

import hashlib
import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from responder.config import Settings, get_settings

logger = logging.getLogger(__name__)

API = "https://api.weixin.qq.com"

# 平台硬限制。**改这两个数等于改平台规则，不许改。**
WINDOW_SECONDS = 48 * 3600      # 用户发消息后 48 小时
MAX_PER_WINDOW = 5              # 该窗口内最多 5 条
EVENT_WINDOW_SECONDS = 60       # 关注/扫码/点菜单后 1 分钟
EVENT_MAX = 3

# 客服消息接口的错误码。每一条都要能说清「现在该做什么」——
# 这条通道上最贵的失败是「接口返回成功、客户什么也没收到」，
# 而它的前一步往往就是这些码被吞掉了。
ERR_HINTS: dict[int, str] = {
    45015: (
        "超出 48 小时回复窗口：客户最后一次发消息已经过去 48 小时以上。"
        "等他再开口窗口就重置——这期间**什么都发不出去**，别重试。"
    ),
    45047: (
        "超出条数限额：客户每发一次消息，我们最多回 5 条。"
        "把话说得更聚合，或调小分条数（`mp_split_max_parts`）。"
    ),
    48001: (
        "这个公众号没有客服消息接口权限。要求是**已认证的服务号**——"
        "去公众平台「设置与开发 → 接口权限」确认「客服消息」是否已获得。"
    ),
    40001: "access_token 无效或过期，一般是 AppSecret 填错了。",
    40003: "openid 不合法——多半是拿了别的公众号的 openid（openid 不跨号通用）。",
    45002: "消息内容超长，需要分条。",
    -1: "微信服务端繁忙，稍后重试即可（这个码是暂时的，不是配置问题）。",
}


def err_hint(payload: dict | int | None) -> str:
    """把错误码翻成一句能照着做的中文；认不出返回空串。

    认不出就沉默：编一句「大概是权限问题」会让人去修错的东西。
    """
    code = payload.get("errcode") if isinstance(payload, dict) else payload
    try:
        return ERR_HINTS.get(int(code), "") if code is not None else ""
    except (TypeError, ValueError):
        return ""


# ---------------------------------------------------------------------------
# 回调验签与解析
# ---------------------------------------------------------------------------
def verify(token: str, signature: str, timestamp: str, nonce: str) -> bool:
    """公众号回调验签：token/timestamp/nonce 排序拼接取 SHA1。

    **token 没配就一律拒绝**（返回 False），与抖音那条通道口径一致。
    留空即放行等于把一个公网地址敞开，任何人都能伪造客户消息灌进来、
    骗走我们的客服消息额度、甚至让 AI 对着伪造的「客户」说话。
    """
    if not token or not signature:
        return False
    raw = "".join(sorted([token, str(timestamp), str(nonce)]))
    return hashlib.sha1(raw.encode()).hexdigest() == signature


@dataclass(frozen=True)
class MpMessage:
    """一条公众号进来的消息或事件。"""

    openid: str                 # 客户在本公众号下的标识
    msg_type: str               # text / image / voice / event ...
    content: str = ""
    msg_id: str = ""
    event: str = ""             # subscribe / SCAN / CLICK ...
    event_key: str = ""
    created_at: datetime | None = None
    raw: dict = field(default_factory=dict)

    @property
    def is_event(self) -> bool:
        return self.msg_type == "event"

    @property
    def is_text(self) -> bool:
        return self.msg_type == "text"

    @property
    def group_id(self) -> str:
        """会话主键，与微信客服/抖音同构：`mp:{appid 简写}:{openid}`。

        由 `parse` 的调用方拼上 appid 前缀；这里给出不带前缀的兜底，
        便于单测。
        """
        return f"mp:{self.openid}"

    @property
    def dedupe_key(self) -> str:
        """幂等键。

        微信在没收到及时响应时**会重推同一条消息（最多三次）**，
        而重推的 MsgId 是相同的。不按它去重的话，客户一句话会被回三遍——
        更糟的是那三遍还各吃掉一条 5 条额度里的份额。
        事件消息没有 MsgId，用 openid+事件+时间戳兜底。
        """
        if self.msg_id:
            return f"mp:{self.msg_id}"
        ts = int(self.created_at.timestamp()) if self.created_at else 0
        return f"mp:{self.openid}:{self.event or self.msg_type}:{ts}"


def parse(body: str | bytes) -> MpMessage | None:
    """把回调 XML 解析成 `MpMessage`。解析不了返回 None（调用方照常回 success）。

    **解析失败绝不抛异常**：微信收不到 200 就会重推，重推三次之后
    它会认为我们的服务挂了。一条读不懂的消息不值得把整条通道拖下水。
    """
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        logger.warning("mp callback: XML 解析失败")
        return None

    def t(tag: str) -> str:
        return (root.findtext(tag) or "").strip()

    created = None
    if raw_ts := t("CreateTime"):
        try:
            created = datetime.fromtimestamp(int(raw_ts))
        except (ValueError, OSError):
            created = None

    openid = t("FromUserName")
    if not openid:
        return None
    return MpMessage(
        openid=openid,
        msg_type=t("MsgType").lower(),
        content=t("Content"),
        msg_id=t("MsgId"),
        event=t("Event"),
        event_key=t("EventKey"),
        created_at=created,
        raw={c.tag: (c.text or "") for c in root},
    )


def passive_text(to_openid: str, from_account: str, text: str) -> str:
    """被动回复用的 XML（在回调响应里直接返回，不消耗 5 条额度）。

    第一期不走这条（见模块开头），但接口留着——将来把确定性话术
    （查订单、问保修这类不需要模型的）改走被动回复，能把额度整个省下来。
    """
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{to_openid}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_account}]]></FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{text}]]></Content>"
        "</xml>"
    )


# ---------------------------------------------------------------------------
# 额度记账
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Budget:
    """这通会话现在还能发几条。"""

    remaining: int
    reason: str = ""

    @property
    def can_send(self) -> bool:
        return self.remaining > 0


def budget(
    last_customer_at: datetime | None,
    sent_since: int,
    *,
    now: datetime | None = None,
    from_event: bool = False,
) -> Budget:
    """算还剩几条额度。**宁可少算一条，也不赌。**

    超发不是「多发了一条」——是接口报错，且累计多了会被平台标记，
    整个号的客服能力一起没。这跟抖音那条通道是同一笔账
    （见 `service._douyin_budget`）。

    `from_event`：这一轮是由关注/扫码/点菜单触发的（窗口 1 分钟、3 条），
      而不是用户发消息（48 小时、5 条）。
    """
    now = now or datetime.now()
    if last_customer_at is None:
        return Budget(0, "客户还没开过口——微信不允许我们主动发起")

    window = EVENT_WINDOW_SECONDS if from_event else WINDOW_SECONDS
    cap = EVENT_MAX if from_event else MAX_PER_WINDOW
    elapsed = (now - last_customer_at).total_seconds()
    if elapsed > window:
        unit = "分钟" if from_event else "小时"
        span = window // 60 if from_event else window // 3600
        return Budget(0, f"超出 {span} {unit}窗口，等客户再开口才能发")
    left = max(0, cap - sent_since)
    if left == 0:
        return Budget(0, f"这一轮的 {cap} 条额度已用完，等客户再开口重置")
    return Budget(left)


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------
class MpClient:
    """公众号 API 客户端。收（回调）在任何模式下都工作，发由管道按模式门控。"""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._token = ""
        self._expiry = 0.0

    def available(self) -> bool:
        return bool(self.settings.mp_app_id and self.settings.mp_app_secret)

    def _access_token(self) -> str:
        """取调用凭据。**走「稳定版」接口，不走 `/cgi-bin/token`。**

        这不是风格偏好，是这个号的现状决定的：酷机时代那个服务号**同时授权给了
        云盛 ERP**（模板消息在那边发）。`/cgi-bin/token` 是「谁最后取，谁的算数」
        ——两套系统轮流刷新，会互相把对方的 token 顶掉。表现极其难查：
        两边都间歇性地报 40001（access_token 无效），重试有时又好了，
        看起来像网络抖动，实际上是两个系统在抢同一把钥匙。

        `/cgi-bin/stable_token` 就是为这种多系统共用一个号的场景做的：
        `force_refresh=false` 时返回当前有效的那一个，**不会让别人的失效**。

        老账号万一没有这个接口，回落到 `/cgi-bin/token`——回落要留痕，
        因为那一刻起「跟云盛抢 token」这个坑就重新打开了。
        """
        if self._token and time.time() < self._expiry:
            return self._token

        data: dict = {}
        try:
            r = httpx.post(
                f"{API}/cgi-bin/stable_token",
                json={
                    "grant_type": "client_credential",
                    "appid": self.settings.mp_app_id,
                    "secret": self.settings.mp_app_secret,
                    "force_refresh": False,
                },
                timeout=10,
            )
            data = r.json()
        except Exception:                              # noqa: BLE001
            logger.warning("stable_token 请求异常，回落 cgi-bin/token")

        if not data.get("access_token"):
            if data:
                logger.warning("stable_token 不可用（%s），回落 cgi-bin/token——"
                               "此后与同号的其他系统会互相顶掉凭据", data)
            r = httpx.get(
                f"{API}/cgi-bin/token",
                params={
                    "grant_type": "client_credential",
                    "appid": self.settings.mp_app_id,
                    "secret": self.settings.mp_app_secret,
                },
                timeout=10,
            )
            data = r.json()

        if not data.get("access_token"):
            hint = err_hint(data)
            raise RuntimeError(f"取 access_token 失败：{data}" + (f"｜{hint}" if hint else ""))
        self._token = data["access_token"]
        # 提前 5 分钟过期，避免边界上拿到一个刚好失效的 token
        self._expiry = time.time() + int(data.get("expires_in", 7200)) - 300
        return self._token

    def send_text(self, openid: str, text: str) -> bool:
        """发一条客服消息。**返回 False 表示这条没发出去**，调用方必须当失败处理。

        与企微那条通道同一条教训：接口成功不等于客户收到，但接口失败
        一定是客户没收到——后者必须落痕，否则库里标着「已发送」而对面是空的。
        """
        if not self.available():
            return False
        try:
            r = httpx.post(
                f"{API}/cgi-bin/message/custom/send",
                params={"access_token": self._access_token()},
                json={
                    "touser": openid,
                    "msgtype": "text",
                    "text": {"content": text},
                },
                timeout=10,
            )
            data = r.json()
        except Exception:
            logger.exception("mp send_text 异常: %s", openid)
            return False
        if data.get("errcode"):
            hint = err_hint(data)
            logger.warning("mp send_text 失败 %s：%s%s", openid, data,
                           f"｜{hint}" if hint else "")
            return False
        return True

    def user_info(self, openid: str) -> dict:
        """取用户昵称等信息。失败返回空字典——这一步锦上添花，不能拖垮主流程。"""
        if not self.available():
            return {}
        try:
            r = httpx.get(
                f"{API}/cgi-bin/user/info",
                params={"access_token": self._access_token(),
                        "openid": openid, "lang": "zh_CN"},
                timeout=10,
            )
            data = r.json()
            return {} if data.get("errcode") else data
        except Exception:
            logger.warning("mp user_info 失败: %s", openid)
            return {}

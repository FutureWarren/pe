"""外部渠道接入口：给 RPA 一类的自动化工具用的通用进出口。

## 为什么要有这一层

律所的获客渠道远多于「有官方 API 的渠道」。微信客服有 API，抖音私信有 API
（等审核），而美团、点评这些既没有开放接口、也不允许把人引到站外——
客户在那儿问了话，只能靠人去后台一条条回。

RPA（影刀、龙虾这类）能替人点那些按钮。但**它只该是一只手，不该是一个脑子**：
如果让 RPA 自己拿话术、自己判断，那每个渠道会长出一套自己的规则，
三个月后同一个「顾问」在五个平台上说话不像同一个人，改一句口径要改五个地方。

所以这里定的边界是硬的：

    RPA 负责搬运（收消息进来、把字打出去），判断/生成/合规/评分/派单全在这一侧。

对接方只需要三个动作：`inbound` 交一条消息、`outbox` 取要说的话、`ack` 报告发出去了。
换一个 RPA 产品，换的只是这三个 HTTP 调用的调用方，我们这侧一行不动。

## 为什么不是「同步返回回复」就完事

最省事的设计是 inbound 直接把回复返回去。但那样**一次网络超时，那句话就永远
消失了，而客户还在等**。所以回复一律先落发件箱（`outbox` 表），inbound 顺手
把当前待发的带回去当快捷路径；没取到也没关系，下一次 `outbox` 还在。
销账要等对方明确 `ack`——宁可重发一句，不可丢一句。

## 鉴权

独立的 `channel_token`，**不复用 admin_token**：RPA 跑在一台随时可能被人碰的
桌面电脑上，令牌等于摊在那儿；共用一个，控制台就跟着一起丢了。
令牌没配 = 接入口关闭（默认拒绝，不是默认放行）。

## 合规

外部渠道的会话在判断层与微信客服完全同构（`GroupProfile.is_kf`），
因此禁止事项闸门、留痕、评分、派单**自动全部生效**，不需要也不允许
在这里另开一条路径。这一点是这个设计最重要的性质：
**多接一个渠道，不多一个合规缺口。**
"""

import hashlib
import logging
import re
import time

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from responder.config import get_settings
from responder.models import ClientStatus, GroupProfile, IncomingMessage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/channel")

# 渠道标识用来拼 group_id，必须是干净的短标识——中文名和空格进了主键，
# 后面每一处按前缀解析的地方都要遭殃。给人看的名字走 label。
_CHANNEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def _auth(settings, token: str) -> None:
    if not settings.channel_enabled:
        raise HTTPException(403, "外部渠道接入未启用")
    if not settings.channel_token:
        # 没配令牌就开放，等于把「给客户发消息」的能力挂在公网上
        raise HTTPException(403, "外部渠道接入未配置令牌（RESPONDER_CHANNEL_TOKEN）")
    if token != settings.channel_token:
        raise HTTPException(401, "渠道令牌不正确")


def _check_channel(channel: str) -> str:
    ch = (channel or "").strip().lower()
    if not _CHANNEL_RE.match(ch):
        raise HTTPException(400, "渠道标识只能用小写字母、数字、下划线、连字符")
    return ch


def _fallback_msg_id(gid: str, content: str) -> str:
    """对方给不出平台消息 id 时，自己造一个**跨进程稳定**的。

    原来用的是内置 `hash()`——Python 每次启动都会换随机种子，
    于是服务器一重启，那头重投的同一条消息就变成了「新消息」，
    客户会被回第二遍。而这类工具本来就以重投为常态。

    用 sha1 保证稳定，再掺一个分钟级时间桶：几秒内的重投一定去重，
    而客户过一会儿真的又发一句一样的话（「在吗」「在吗」）仍然答得上。
    宁可多答一句，不可把客户晾在那儿。
    """
    bucket = int(time.time()) // 60
    digest = hashlib.sha1(f"{gid}|{content}|{bucket}".encode()).hexdigest()[:16]
    return f"{gid}:auto:{digest}"


def group_id_for(channel: str, external_id: str) -> str:
    return f"ch:{channel}:{external_id}"


class Inbound(BaseModel):
    channel: str  # 渠道标识，如 meituan
    external_id: str  # 客户在该渠道的标识（用户名/会话号皆可，只要稳定）
    content: str = ""
    msg_id: str = ""  # 平台侧消息 id；给不出就留空，我们自己造一个
    name: str = ""  # 客户昵称，用于建档展示
    msg_type: str = "text"
    is_staff: bool = False  # 这条是我方（人工客服）发的
    label: str = ""  # 渠道展示名，如「美团-静安店」
    take: int = Field(default=5, ge=0, le=20)  # 顺带取回几条待发


@router.post("/inbound")
def inbound(
    body: Inbound, request: Request,
    x_channel_token: str = Header(default=""),
):
    """交进来一条客户消息，顺手带走当前要说的话。

    返回的 `replies` 是**快捷路径**，不是唯一路径：拿不到（网络抖动、
    模型慢）就下一次调 `/channel/outbox`，话还在那儿。
    """
    settings = get_settings()
    _auth(settings, x_channel_token)
    channel = _check_channel(body.channel)
    external_id = (body.external_id or "").strip()
    if not external_id:
        raise HTTPException(400, "external_id 不能为空")

    store = request.app.state.store
    pipeline = request.app.state.pipeline
    gid = group_id_for(channel, external_id)

    # 心跳先记：哪怕这条消息后面处理失败了，「那头还活着」也是真的
    store.touch_channel(channel, inbound=True, label=body.label)

    group = store.get_group(gid)
    if group is None:
        # 首次进线自动建档。客户在美团上问一句就是个陌生人，不该要求
        # 谁先去后台手工建一条记录——那一步没人会做。
        store.upsert_group(GroupProfile(
            group_id=gid,
            name=body.name or f"{body.label or channel}客户",
            client_status=ClientStatus.PROSPECT,
            ext_channel=channel,
            ext_user_id=external_id,
            case_type=settings.kf_default_case_type,
            lawyer_name=settings.kf_default_lawyer_name,
            # 提醒接收人必须在建档时就落下。外部渠道没有「接待人」可查，
            # 只能用全局兜底——这一行留空的后果是**线索照样入库评分，
            # 但那张交接单一个人也收不到**，而控制台里看什么都正常。
            lawyer_userid=settings.default_notify_userid,
        ))
    elif body.name and not group.name:
        group.name = body.name
        store.upsert_group(group)

    content = (body.content or "").strip()
    if content:
        msg = IncomingMessage(
            msg_id=body.msg_id.strip() or _fallback_msg_id(gid, content),
            group_id=gid,
            sender_id=external_id if not body.is_staff else "staff",
            sender_is_staff=body.is_staff,
            content=content,
            msg_type=body.msg_type or "text",
        )
        # save_message 返回 False = 这条已经处理过（那头重试是常态，不是异常）
        if store.save_message(msg):
            try:
                pipeline.handle(msg)
            except Exception:
                # 判断链炸了不能让那头以为消息没送到——它会一直重试同一条
                logger.exception("channel inbound pipeline failed: %s", gid)

    return {
        "ok": True,
        "group_id": gid,
        "replies": _outbox_payload(store, gid, body.take),
    }


def _outbox_payload(store, gid: str, limit: int) -> list[dict]:
    if limit <= 0:
        return []
    return [
        {"id": r["id"], "text": r["text"]}
        for r in store.pending_outbound(gid, limit=limit)
    ]


@router.get("/outbox")
def outbox(
    request: Request, channel: str, external_id: str, take: int = 5,
    x_channel_token: str = Header(default=""),
):
    """取当前该对这个客户说的话。不销账——销账等 `/channel/ack`。"""
    settings = get_settings()
    _auth(settings, x_channel_token)
    ch = _check_channel(channel)
    store = request.app.state.store
    store.touch_channel(ch)
    gid = group_id_for(ch, (external_id or "").strip())
    return {"ok": True, "group_id": gid,
            "replies": _outbox_payload(store, gid, max(0, min(take, 20)))}


@router.get("/pending")
def pending(
    request: Request, channel: str = "", limit: int = 50,
    x_channel_token: str = Header(default=""),
):
    """现在有哪些客户在等我们说话。

    **主动发起的那几句全靠这个接口。** 客户聊了一半不说话了，系统会生成
    一句挽留；可对外部渠道来说，那句话排进发件箱之后没有任何人会来取——
    除非客户自己再开口，而他要是再开口，挽留本身就没意义了。
    所以那头必须定期来问一声「谁在等」，不能只在收到消息时才来。
    """
    settings = get_settings()
    _auth(settings, x_channel_token)
    ch = _check_channel(channel) if channel else ""
    store = request.app.state.store
    if ch:
        store.touch_channel(ch)
    rows = store.pending_outbound_targets(ch, limit=max(1, min(limit, 200)))
    return {"ok": True, "conversations": [
        {
            "group_id": r["group_id"],
            "external_id": r.get("ext_user_id") or "",
            "channel": r.get("ext_channel") or ch,
            "count": r["n"],
        }
        for r in rows if r.get("ext_user_id")
    ]}


class Ack(BaseModel):
    ids: list[int]
    channel: str = ""


@router.post("/ack")
def ack(body: Ack, request: Request, x_channel_token: str = Header(default="")):
    """确认这几条真的发到客户那儿了。没 ack 的下次照样取得到。"""
    settings = get_settings()
    _auth(settings, x_channel_token)
    store = request.app.state.store
    if body.channel:
        store.touch_channel(_check_channel(body.channel))
    return {"ok": True, "acked": store.ack_outbound(body.ids)}


class Heartbeat(BaseModel):
    channel: str
    label: str = ""


@router.post("/heartbeat")
def heartbeat(
    body: Heartbeat, request: Request, x_channel_token: str = Header(default=""),
):
    """那头没消息可交时也定期来报个到。

    没有这个，「一整天没有客户」和「RPA 三天前就挂了」在数据上长得一模一样，
    而后者每多一天都是真金白银。
    """
    settings = get_settings()
    _auth(settings, x_channel_token)
    store = request.app.state.store
    store.touch_channel(_check_channel(body.channel), label=body.label)
    return {"ok": True}

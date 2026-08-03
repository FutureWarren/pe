"""企微回调入口 + 通用 JSON 摄入端点。

- GET /wecom/callback: 企微后台 URL 验证（echostr 解密回显）
- POST /wecom/callback: 加密 XML 消息回调
- POST /ingest: JSON 摄入（会话存档拉取器 / 影子模式回放 / 测试用）
  [待定] 客户群消息的最终获取方式取决于律所侧企微配置（会话存档 or 群机器人）。
"""

import json
import logging
import time
import xml.etree.ElementTree as ET

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse

from responder.config import get_settings
from responder.gateway import bot, douyin
from responder.gateway.wecom_crypto import WeComCrypto
from responder.models import IncomingMessage
from responder.service import Pipeline
from responder.worker import KfSyncJob

logger = logging.getLogger(__name__)

router = APIRouter()


def get_crypto() -> WeComCrypto:
    s = get_settings()
    return WeComCrypto(s.wecom_token, s.wecom_encoding_aes_key, s.wecom_corp_id)


def get_pipeline(request: Request) -> Pipeline:
    return request.app.state.pipeline


@router.get("/wecom/callback")
def verify_url(
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
    echostr: str = Query(...),
    crypto: WeComCrypto = Depends(get_crypto),
):
    if not crypto.verify(msg_signature, timestamp, nonce, echostr):
        return Response(status_code=403)
    return Response(content=crypto.decrypt(echostr), media_type="text/plain")


@router.post("/wecom/callback")
async def receive(
    request: Request,
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
    crypto: WeComCrypto = Depends(get_crypto),
    pipeline: Pipeline = Depends(get_pipeline),
):
    body = await request.body()
    try:
        encrypt = ET.fromstring(body).findtext("Encrypt", "")
    except ET.ParseError:
        return Response(status_code=400)
    if not crypto.verify(msg_signature, timestamp, nonce, encrypt):
        return Response(status_code=403)

    try:
        xml = ET.fromstring(crypto.decrypt(encrypt))
    except (ET.ParseError, ValueError):
        # 签名已验真但内容异常：回 success 避免企微按超时重发，仅记日志排查
        logger.warning("undecodable callback payload, ts=%s nonce=%s", timestamp, nonce)
        return Response(content="success", media_type="text/plain")
    worker = getattr(request.app.state, "worker", None)
    async_ok = worker is not None and pipeline.settings.callback_async

    if xml.findtext("MsgType") == "text":
        msg = IncomingMessage(
            msg_id=xml.findtext("MsgId") or f"{timestamp}-{nonce}",
            group_id=xml.findtext("ChatId") or xml.findtext("ToUserName") or "",
            sender_id=xml.findtext("FromUserName") or "",
            content=xml.findtext("Content") or "",
            msg_type="text",
        )
        # 企微要求 5 秒内应答，判断链路含 LLM 调用与分条发送延时，必须异步处理；
        # 超时会触发企微重发回调 → 重复处理 → 群里重复说话（worker 以 msg_id 去重兜底）
        if async_ok:
            worker.submit(msg)
        else:
            pipeline.handle(msg)
    elif xml.findtext("Event") == "kf_msg_or_event":
        # 微信客服：回调只带一个 10 分钟有效的 Token，真实消息要用它去 sync_msg 拉
        job = KfSyncJob(
            token=xml.findtext("Token") or "",
            open_kfid=xml.findtext("OpenKfId") or "",
        )
        if async_ok:
            worker.submit(job)
        else:
            worker.process_kf(job) if worker else None
    # 回复走主动发送通道，回调只回 success
    return Response(content="success", media_type="text/plain")


def get_bot_crypto() -> WeComCrypto:
    """智能机器人有独立的 Token / EncodingAESKey（后台创建机器人时生成）。"""
    s = get_settings()
    return WeComCrypto(s.wecom_bot_token, s.wecom_bot_aes_key, s.wecom_corp_id)


@router.get("/wecom/bot/callback")
def verify_bot_url(
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
    echostr: str = Query(...),
    crypto: WeComCrypto = Depends(get_bot_crypto),
):
    if not crypto.verify(msg_signature, timestamp, nonce, echostr):
        return Response(status_code=403)
    return Response(content=crypto.decrypt(echostr), media_type="text/plain")


@router.post("/wecom/bot/callback")
async def receive_bot(
    request: Request,
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
    crypto: WeComCrypto = Depends(get_bot_crypto),
    pipeline: Pipeline = Depends(get_pipeline),
):
    """群聊 @ 智能机器人 / 与机器人单聊的消息回调。

    与应用回调是两套凭据、两条路由，但共用同一条判断与话术管道。
    """
    body = await request.body()
    try:
        encrypt = ET.fromstring(body).findtext("Encrypt", "")
    except ET.ParseError:
        return Response(status_code=400)
    if not crypto.verify(msg_signature, timestamp, nonce, encrypt):
        return Response(status_code=403)
    try:
        xml = ET.fromstring(crypto.decrypt(encrypt))
    except (ET.ParseError, ValueError):
        logger.warning("undecodable bot callback, ts=%s nonce=%s", timestamp, nonce)
        return Response(content="success", media_type="text/plain")

    if pipeline.settings.bot_enabled:
        env = bot.parse(xml, fallback_msg_id=f"bot-{timestamp}-{nonce}")
        if env is not None:
            worker = getattr(request.app.state, "worker", None)
            if worker is not None and pipeline.settings.callback_async:
                worker.submit(env)
            elif worker is not None:
                worker.process_bot(env)
            elif env.msg is not None:
                pipeline.handle(env.msg)
    return Response(content="success", media_type="text/plain")


@router.post("/douyin/callback")
async def receive_douyin(request: Request, pipeline: Pipeline = Depends(get_pipeline)):
    """抖音企业号私信回调。

    与企微的两点不同，决定了这里不能照抄上面的写法：
      1. 报文是 JSON，签名放在 **HTTP 头**（X-Douyin-Signature），不在查询串里；
      2. 配置回调地址时平台先发一个挑战包，必须原样回显 challenge 才算配置成功——
         这一步不通，后面什么都收不到。

    未配置校验 Token 时不做签名校验（本机联调用）。生产必须配，
    否则任何人都能往这个地址灌消息，让 AI 对着伪造的客户说话。
    """
    s = pipeline.settings
    body = await request.body()
    if s.douyin_callback_token and not douyin.verify_signature(
        s.douyin_callback_token, request.headers, body
    ):
        return Response(status_code=403)
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        return Response(status_code=400)
    if not isinstance(payload, dict):
        return Response(status_code=400)

    env = douyin.parse(payload, fallback_msg_id=f"dy-{int(time.time() * 1000)}")
    if env is not None and env.challenge is not None:
        return JSONResponse({"challenge": env.challenge})
    if env is not None and s.douyin_enabled:
        worker = getattr(request.app.state, "worker", None)
        if worker is not None and s.callback_async:
            worker.submit(env)
        elif worker is not None:
            worker.process_douyin(env)
    # 抖音同样按超时重推，任何情况下都要立刻回 200
    return JSONResponse({"err_no": 0, "err_msg": "success"})


@router.post("/ingest")
def ingest(msg: IncomingMessage, request: Request, seconds_unanswered: float = 0.0,
           pipeline: Pipeline = Depends(get_pipeline)):
    from responder.console.api import require_admin

    require_admin(request, request.headers.get("x-admin-token"))
    decision = pipeline.handle(msg, seconds_unanswered=seconds_unanswered)
    return decision.model_dump()

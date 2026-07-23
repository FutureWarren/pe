"""企微回调入口 + 通用 JSON 摄入端点。

- GET /wecom/callback: 企微后台 URL 验证（echostr 解密回显）
- POST /wecom/callback: 加密 XML 消息回调
- POST /ingest: JSON 摄入（会话存档拉取器 / 影子模式回放 / 测试用）
  [待定] 客户群消息的最终获取方式取决于律所侧企微配置（会话存档 or 群机器人）。
"""

import xml.etree.ElementTree as ET

from fastapi import APIRouter, Depends, Query, Request, Response

from responder.config import get_settings
from responder.gateway.wecom_crypto import WeComCrypto
from responder.models import IncomingMessage
from responder.service import Pipeline

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
    encrypt = ET.fromstring(body).findtext("Encrypt", "")
    if not crypto.verify(msg_signature, timestamp, nonce, encrypt):
        return Response(status_code=403)

    xml = ET.fromstring(crypto.decrypt(encrypt))
    if xml.findtext("MsgType") == "text":
        msg = IncomingMessage(
            msg_id=xml.findtext("MsgId") or f"{timestamp}-{nonce}",
            group_id=xml.findtext("ChatId") or xml.findtext("ToUserName") or "",
            sender_id=xml.findtext("FromUserName") or "",
            content=xml.findtext("Content") or "",
            msg_type="text",
        )
        pipeline.handle(msg)
    # 企微要求 5 秒内响应；回复走主动发送通道，回调只回 success
    return Response(content="success", media_type="text/plain")


@router.post("/ingest")
def ingest(msg: IncomingMessage, request: Request, seconds_unanswered: float = 0.0,
           pipeline: Pipeline = Depends(get_pipeline)):
    from responder.console.api import require_admin

    require_admin(request, request.headers.get("x-admin-token"))
    decision = pipeline.handle(msg, seconds_unanswered=seconds_unanswered)
    return decision.model_dump()

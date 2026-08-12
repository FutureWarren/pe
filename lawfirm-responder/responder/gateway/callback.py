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
    request: Request,
    msg_signature: str = Query(default=""),
    timestamp: str = Query(default=""),
    nonce: str = Query(default=""),
    echostr: str = Query(default=""),
    crypto: WeComCrypto = Depends(get_crypto),
):
    """企微的 URL 验证；**人用浏览器打开时给一张能看懂的自检页**。

    为什么值得写：排查「客户消息为什么没进来」时，第一件事就是拿浏览器
    戳一下这个地址看它通不通。参数缺失时 FastAPI 默认回一屏
    `{"detail":[{"type":"missing"...}]}`——那对律所方等于乱码，
    而它其实是个**好消息**（地址通了、路由到了我们）。
    顺手把管道计数一并摆出来：一个网址就能查完，不用两处对照。
    """
    if not (msg_signature and timestamp and nonce and echostr):
        store = request.app.state.store
        c = store.counters()

        def n(key: str) -> int:
            return int((c.get(key) or {}).get("n", 0))

        total, synced = n("kf_cb_total"), n("kf_synced")
        if total == 0:
            verdict = ("❌ 企业微信从来没有往这个地址推过消息。\n"
                       "   去「企业微信管理后台 → 应用管理 → 微信客服 → API」，\n"
                       "   把「接收事件服务器」的 URL 填成本页地址，\n"
                       "   Token 与 EncodingAESKey 用自建应用那一套。")
        elif n("kf_cb_bad_signature") > 0 and n("kf_cb_event") == 0:
            verdict = ("❌ 推过来了，但签名一直对不上。\n"
                       "   Token 或 EncodingAESKey 跟企微后台填的不一致，去核对这两个值。")
        elif n("kf_cb_event") > 0 and synced == 0:
            verdict = ("❌ 收到通知了，但一条消息也拉不回来。\n"
                       "   多半是 Secret 变了或客服账号没交给这个应用管（48007）。")
        else:
            verdict = "✅ 消息进得来。"
        body = (
            "这是给企业微信用的回调地址，不是给人看的页面。\n"
            "你能看到这段字，说明**这个地址从公网是通的**。\n\n"
            f"{verdict}\n\n"
            "———— 管道分段计数 ————\n"
            f"企微推过来的回调总数      {total}\n"
            f"  其中签名对不上          {n('kf_cb_bad_signature')}\n"
            f"  其中是微信客服事件      {n('kf_cb_event')}\n"
            f"据此拉回来的消息条数      {synced}\n\n"
            f"最后一次收到的回调        {store.get_note('kf_cb_last') or '（没有）'}\n"
            f"最后一次拉到的消息        {store.get_note('kf_synced_last') or '（没有）'}\n"
            f"认不出的事件名            {store.get_note('kf_unknown_event') or '（没有）'}\n"
        )
        return Response(content=body, media_type="text/plain; charset=utf-8")
    if not crypto.verify(msg_signature, timestamp, nonce, echostr):
        request.app.state.store.bump("kf_verify_failed")
        return Response(status_code=403)
    request.app.state.store.bump("kf_verify_ok")
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
    # 管道计数器：把「企微到底有没有推给我们」这件事变成一个看得见的数。
    # 2026-08-11 真机——客户连发多条消息一条回复也没有，而服务器一切正常
    # （线程活着、队列空、通道配置有效）。当时分不清「没推给我们」
    # 「推了但签名验不过」「验过了但拉不到消息」，三者症状完全一样。
    store = request.app.state.store
    store.bump("kf_cb_total")
    body = await request.body()
    try:
        encrypt = ET.fromstring(body).findtext("Encrypt", "")
    except ET.ParseError:
        store.bump("kf_cb_unparsable")
        return Response(status_code=400)
    if not crypto.verify(msg_signature, timestamp, nonce, encrypt):
        # 签名对不上 = Token/EncodingAESKey 跟企微后台填的不一致。
        # 这条要是在涨，就别去查判断层了，去核对那两个值。
        store.bump("kf_cb_bad_signature")
        return Response(status_code=403)

    try:
        xml = ET.fromstring(crypto.decrypt(encrypt))
    except (ET.ParseError, ValueError):
        # 签名已验真但内容异常：回 success 避免企微按超时重发，仅记日志排查
        logger.warning("undecodable callback payload, ts=%s nonce=%s", timestamp, nonce)
        return Response(content="success", media_type="text/plain")
    worker = getattr(request.app.state, "worker", None)
    async_ok = worker is not None and pipeline.settings.callback_async
    # 记下最后一次收到的是什么，认不出的类型也留个证据
    store.set_note(
        "kf_cb_last",
        f"{xml.findtext('MsgType') or '-'}/{xml.findtext('Event') or '-'}",
    )

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
        store.bump("kf_cb_event")
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
    # 管道计数器：把「企微到底有没有推给我们」这件事变成一个看得见的数。
    # 2026-08-11 真机——客户连发多条消息一条回复也没有，而服务器一切正常
    # （线程活着、队列空、通道配置有效）。当时分不清「没推给我们」
    # 「推了但签名验不过」「验过了但拉不到消息」，三者症状完全一样。
    store = request.app.state.store
    store.bump("kf_cb_total")
    body = await request.body()
    try:
        encrypt = ET.fromstring(body).findtext("Encrypt", "")
    except ET.ParseError:
        store.bump("kf_cb_unparsable")
        return Response(status_code=400)
    if not crypto.verify(msg_signature, timestamp, nonce, encrypt):
        # 签名对不上 = Token/EncodingAESKey 跟企微后台填的不一致。
        # 这条要是在涨，就别去查判断层了，去核对那两个值。
        store.bump("kf_cb_bad_signature")
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

    **没配校验 Token 就整个拒收**（默认拒绝，不是默认放行）。
    原来是「没配就不校验」，那等于把一个公网地址敞开：任何人都能灌进伪造的
    客户消息，让 AI 对着不存在的人说话、生成假线索、占满律师的队列。
    「本机联调方便」不值这个风险——联调时把 token 配上同样方便。
    与外部渠道接入口（`gateway/channel.py`）口径一致。
    """
    s = pipeline.settings
    body = await request.body()
    if not s.douyin_callback_token:
        logger.warning("douyin callback rejected: RESPONDER_DOUYIN_CALLBACK_TOKEN 未配置")
        return Response(status_code=403)
    if not douyin.verify_signature(s.douyin_callback_token, request.headers, body):
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

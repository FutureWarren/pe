"""FastAPI 应用组装与启动入口。"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from responder import ops
from responder.config import get_settings
from responder.console.api import router as console_router
from responder.console.api import ui_router
from responder.engine import llm
from responder.gateway.callback import router as callback_router
from responder.gateway.channel import router as channel_router
from responder.gateway.douyin import DouyinClient
from responder.gateway.sender import WeComSender
from responder.gateway.wecom_kf import KfClient
from responder.service import Pipeline
from responder.store.db import Store
from responder.worker import Worker

logging.basicConfig(level=logging.INFO)


def create_app() -> FastAPI:
    settings = get_settings()
    store = Store(settings.db_path)
    # 通道对象始终构建（token 惰性获取），由 Pipeline 按模式实时门控是否真的发出，
    # 这样控制台切换影子/正式模式无需重启服务
    sender = WeComSender(settings)
    # 客服 client 在任何模式下都要能「收」（拉消息），「发」同样由 Pipeline 门控
    kf_client = KfClient(settings) if settings.wecom_kf_secret else None
    # 抖音私信：凭据齐备才构建。收由回调驱动（无需轮询），发同样受模式门控。
    dy_client = DouyinClient(settings) if settings.douyin_client_key else None
    pipeline = Pipeline(store, sender, settings, kf_client=kf_client,
                        douyin_client=dy_client)
    worker = Worker(pipeline, store, sender,
                    poll_seconds=settings.worker_poll_seconds, kf_client=kf_client,
                    douyin_client=dy_client)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        worker.start()
        yield
        worker.stop()

    app = FastAPI(title="律所群 AI 第一响应助手", version="0.2.0", lifespan=lifespan)
    app.state.store = store
    app.state.pipeline = pipeline
    app.state.worker = worker
    app.include_router(callback_router)
    app.include_router(channel_router)
    app.include_router(console_router)
    app.include_router(ui_router)

    @app.get("/health")
    def health():
        provider = llm.resolve(settings)
        return {
            "ok": True,
            "mode": settings.mode,
            "version": app.version,
            # 已部署的提交号：远程升级后据此确认新版是否真的生效
            "commit": ops.current_commit(settings.update_repo_dir),
            "llm": f"{provider.name}:{provider.model}" if provider else "rules-only",
            "kf": bool(kf_client and kf_client.available()),
            "douyin": bool(dy_client and dy_client.available()),
            "queued": worker.qsize(),
            # 进线事件累计条数。「进线即问候」整条链路挂在企微推这个事件上，
            # 一条都没有就说明企微根本没通知过我们「有人进来了」——那跟
            # 「新版没部署」在客户那边看起来一模一样，得有个数才分得清。
            # 放在这个免鉴权端点上，是因为控制台要带令牌头、手机上打不开；
            # 一个整数不含任何客户信息，代价是零。
            "enter_events": store.count_event_messages(),
            # 运维指令执行情况。「什么都没收到」有两种完全不同的原因，
            # 这两个数把它们分开：ops_done=0 → 指令还没跑（新版没上或没到点）；
            # ops_done>0 且 ops_error 非空 → 跑了，但企微把消息拒了（码在 error 里）。
            # 没有这两个数，远程排障只能靠猜。
            "ops_done": store.count_commands_done(),
            "ops_error": store.get_note("ops_error"),
        }

    return app


def serve() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(create_app(), host=settings.api_host, port=settings.api_port)

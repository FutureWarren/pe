"""FastAPI 应用组装与启动入口。"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from responder.config import get_settings
from responder.console.api import router as console_router
from responder.console.api import ui_router
from responder.engine import llm
from responder.gateway.callback import router as callback_router
from responder.gateway.sender import WeComSender
from responder.gateway.wecom_kf import KfClient
from responder.service import Pipeline
from responder.store.db import Store
from responder.worker import Worker

logging.basicConfig(level=logging.INFO)


def create_app() -> FastAPI:
    settings = get_settings()
    store = Store(settings.db_path)
    sender = WeComSender(settings) if settings.mode == "live" else None
    # 客服 client 在任何模式下都要能「收」（拉消息），「发」由 Pipeline 按模式门控
    kf_client = KfClient(settings) if settings.wecom_kf_secret else None
    pipeline = Pipeline(store, sender, settings, kf_client=kf_client)
    worker = Worker(pipeline, store, pipeline.sender,
                    poll_seconds=settings.worker_poll_seconds, kf_client=kf_client)

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
    app.include_router(console_router)
    app.include_router(ui_router)

    @app.get("/health")
    def health():
        provider = llm.resolve(settings)
        return {
            "ok": True,
            "mode": settings.mode,
            "version": app.version,
            "llm": f"{provider.name}:{provider.model}" if provider else "rules-only",
            "kf": bool(kf_client and kf_client.available()),
            "queued": worker.qsize(),
        }

    return app


def serve() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(create_app(), host=settings.api_host, port=settings.api_port)

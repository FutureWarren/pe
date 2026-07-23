"""FastAPI 应用组装与启动入口。"""

import logging

from fastapi import FastAPI

from responder.config import get_settings
from responder.console.api import router as console_router
from responder.gateway.callback import router as callback_router
from responder.gateway.sender import WeComSender
from responder.service import Pipeline
from responder.store.db import Store

logging.basicConfig(level=logging.INFO)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="律所群 AI 第一响应助手", version="0.1.0")
    store = Store(settings.db_path)
    sender = WeComSender(settings) if settings.mode == "live" else None
    app.state.store = store
    app.state.pipeline = Pipeline(store, sender, settings)
    app.include_router(callback_router)
    app.include_router(console_router)

    @app.get("/health")
    def health():
        return {"ok": True, "mode": settings.mode}

    return app


def serve() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(create_app(), host=settings.api_host, port=settings.api_port)

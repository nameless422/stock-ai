from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from db.schema import init_db
from app.config import settings
from app.routers.api import router as api_router
from app.routers.web import router as web_router
from app.services.screener_service import bootstrap_task_system


@asynccontextmanager
async def lifespan(app: FastAPI):
    limits = httpx.Limits(max_connections=100, max_keepalive_connections=20)
    timeout = httpx.Timeout(connect=10.0, read=15.0, write=10.0, pool=5.0)
    app.state.market_http_client = httpx.AsyncClient(
        timeout=timeout,
        limits=limits,
        headers={"User-Agent": "stock-ai/1.0"},
        follow_redirects=True,
    )
    try:
        yield
    finally:
        client = app.state.market_http_client
        app.state.market_http_client = None
        if client is not None:
            await client.aclose()


def create_app() -> FastAPI:
    init_db(settings.db_path)
    bootstrap_task_system()
    app = FastAPI(title="股票K线AI分析", description="A股实时数据 + K线图 + AI决策建议", lifespan=lifespan)
    app.include_router(web_router)
    app.include_router(api_router)
    app.mount("/static", StaticFiles(directory=str(settings.base_dir / "static")), name="static")
    return app
